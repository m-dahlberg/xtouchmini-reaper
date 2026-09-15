"""Turn controller events into REAPER messages, and REAPER state into LEDs.

The engine owns no I/O. It is handed a thing to send OSC with and a thing to
send MIDI with, so the whole of it can be driven from tests with a fake clock
and a recorder -- the same shape as the ShuttleXpress bridge's engine.

What it does not do: decide which plugin is active, or map a parameter by name.
That lives in the panel, and arrives here already resolved in state.json.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from . import protocol as P
from .protocol import ButtonPress, EncoderPush, EncoderTurn, FaderMove, LedState
from .state import ABSOLUTE, PICKUP, RELATIVE, State


@dataclass
class Feel:
    """How the controls behave. Overridable from config.toml."""
    # One detent moves this much of a parameter's 0..1 range. The hardware
    # already accelerates -- a fast turn sends 3 where a slow one sends 1 --
    # so the engine must not apply a second curve on top.
    coarse_step: float = 1.0 / 128.0
    fine_step: float = 1.0 / 1024.0
    # How close the fader must come to the current volume to take over.
    pickup_epsilon: float = 0.02
    # A push shorter than this that moved nothing is a reset, not a fine-hold.
    reset_max_hold: float = 0.6
    # How much a tapered detent is slowed at the fine end of its travel.
    # 0.25 means a quarter speed there, full speed at the other end.
    taper_floor: float = 0.25
    # How often to repaint the lamps unconditionally. The layer button sends
    # no MIDI at all (verified with a raw capture), and the device keeps LED
    # state per layer, so switching layers leaves every lamp dark and the
    # daemon has no way to know until something on the new layer is touched.
    # A slow repaint puts them back. 24 messages at this interval, against the
    # ~700 a second the un-deduplicated repaint used to send.
    led_refresh_interval: float = 1.0
    # After we write a parameter, ignore incoming values for it for this long.
    # The panel republishes state.json whenever a value changes, so a turn can
    # race its own echo: we send 0.55, the panel is still reporting 0.52, and
    # the next detent starts from the stale figure and jumps backwards. That
    # reads as an encoder that moves both ways at once.
    echo_suppress: float = 0.5


# How close to an end the volume counts as being AT it, for the purpose of
# _pinned_together. One fader step, because that is the smallest move the
# hardware can make: a volume within one step of silence is silent as far as
# anything the user can do about it is concerned.
SYNC_EPSILON = 1.0 / 127.0


# REAPER's OSC addresses are 1-based; ReaScript indices are 0-based. state.json
# carries ReaScript's numbering, so the conversion happens here, once.
def _osc_fx(index: int) -> int:
    return index + 1


def _osc_param(index: int) -> int:
    return index + 1


class Engine:
    def __init__(self, osc, midi, feel: Feel | None = None,
                 clock=time.monotonic):
        self.osc = osc                  # .send(address, *args)
        self.midi = midi                # .send(list[int])
        self.feel = feel or Feel()
        self.clock = clock

        self.state = State()
        self._held: dict[tuple[str, int], float] = {}   # push -> time pressed
        self._turned_while_held: set = set()
        self._toggles: dict[tuple[str, int], bool] = {}
        self._abs_position: dict[tuple[str, int], int] = {}

        self._written_at: dict[tuple, float] = {}
        # Last payload sent to each LED. refresh_leds repaints the whole
        # surface, and the panel republishes state.json on every value change,
        # so without this one knob turn floods the device with 24 messages a
        # frame -- roughly 700 a second at panel refresh rate.
        self._led_cache: dict[tuple, int] = {}
        self._last_repaint = 0.0
        self._debug = None
        self._osc_debug = None
        self._fader_debug = None
        self._fader_engaged = False
        self._fader_last: float | None = None
        self._volume = 0.0
        self._pickup_hint = 0            # -1 move down, +1 move up, 0 engaged
        self._volume_written_at = -1e9

        # Relative mode only. See _on_fader_relative for what they mean.
        self._rel_raw: int | None = None
        self._rel_paused = False
        self._rel_recal = 0              # 0, or the direction of the walk-back
        self._rel_moved_at = 0.0
        self._fader_mode = PICKUP
        self._visible_layer = P.LAYER_A

        # Which plugin REAPER's parameter feedback is currently about. See
        # _feedback_is_for_focused_fx -- this is the only thing that makes the
        # feedback safe to apply.
        self._reaper_fx_number: int | None = None
        self._reaper_fx_name: str | None = None
        self._reaper_track_number: int | None = None
        self._dropped_feedback_for: tuple | None = None

    # --- state ------------------------------------------------------------

    def set_state(self, state: State) -> None:
        """Adopt a new state.json. Resets anything that was about the old FX."""
        changed_track = state.track.index != self.state.track.index
        previous = self.state
        self._carry_recent_values(previous, state)
        self.state = state
        # Logged with the old value beside the new one, because the two ought
        # to be in the same units and the single most expensive way for them
        # not to be is silently.
        if self._adopt_volume():
            if abs(state.track.volume - self._volume) > 1e-9:
                self._flog(f"shadow <- state.json {state.track.volume:.4f} "
                           f"(was {self._volume:.4f})")
            self._volume = state.track.volume
        elif abs(state.track.volume - self._volume) > 1e-9:
            self._flog(f"shadow keeps {self._volume:.4f}, state.json says "
                       f"{state.track.volume:.4f} (echo window)")
        if state.fader.mode != self._fader_mode:
            # Whatever was half-done belonged to the old mode: a pickup that
            # had engaged, a pause waiting to be walked back. The physical
            # position is the one thing that survives, because the fader has
            # not moved.
            self._fader_mode = state.fader.mode
            self._fader_engaged = False
            self._pickup_hint = 0
            self._rel_paused = False
            self._rel_recal = 0
        if changed_track:
            # The fader is not motorised, so it is now somewhere that has
            # nothing to do with this track's level. Make it pick up again.
            self._fader_engaged = False
        self._toggles.clear()
        self.refresh_leds()

    def _carry_recent_values(self, previous: State, incoming: State) -> None:
        """Keep our own value for any slot we wrote in the last moment.

        Without this the controller fights its own echo: the panel's view of a
        parameter lags the write that caused it, and adopting the lagging value
        makes the next detent start from the wrong place.
        """
        now = self.clock()
        for key, written in list(self._written_at.items()):
            if now - written > self.feel.echo_suppress:
                del self._written_at[key]
                continue
            layer, kind, index = key
            ours = previous.slot(layer, kind, index)
            theirs = incoming.slot(layer, kind, index)
            # Only if it is still the same parameter -- a remap or an FX change
            # means their value is the authority, not ours.
            if ours is None or theirs is None or ours.param != theirs.param:
                continue
            try:
                incoming.layers[layer][kind][index] = replace(
                    theirs, value=ours.value)
            except (KeyError, IndexError):
                pass

    def set_debug(self, sink) -> None:
        """Route decoded events to `sink` for logging."""
        self._debug = sink

    def set_osc_debug(self, sink) -> None:
        """Route every inbound OSC message to `sink`.

        Separate from set_debug because it is a different order of noise: one
        focus change is a burst of three messages per parameter, 128 parameters
        deep. Worth having when the question is what REAPER actually said, and
        not worth having any other time.
        """
        self._osc_debug = sink

    # --- inbound from the controller --------------------------------------

    def handle(self, event) -> None:
        if self._debug is not None:
            self._debug(event)
        # The hardware switches layers on its own and says so only by changing
        # which CC and note numbers it sends. Tell the panel, so its display
        # follows the device.
        layer = getattr(event, "layer", None)
        if layer is not None and layer != self._visible_layer:
            self.set_visible_layer(layer)
            command = self.state.notify.for_layer(layer)
            if command:
                self.osc.send(f"/action/{command}")
        if isinstance(event, EncoderTurn):
            self._on_turn(event)
        elif isinstance(event, EncoderPush):
            self._on_push(event)
        elif isinstance(event, ButtonPress):
            self._on_button(event)
        elif isinstance(event, FaderMove):
            self._on_fader(event)

    def _on_turn(self, event: EncoderTurn) -> None:
        key = (event.layer, event.index)
        if key in self._held:
            self._turned_while_held.add(key)

        delta = event.delta
        if event.absolute is not None:
            # Absolute encoders have no delta of their own. Diff against the
            # last position; the first message after a jump is swallowed
            # because its "delta" would be the size of the jump.
            previous = self._abs_position.get(key)
            self._abs_position[key] = event.absolute
            if previous is None:
                return
            delta = event.absolute - previous
        if delta == 0:
            return

        slot = self.state.slot(event.layer, "encoders", event.index)
        if slot is None:
            return
        step = self.feel.fine_step if key in self._held else self.feel.coarse_step
        step *= slot.sensitivity * self._taper_scale(slot)
        value = _clamp(slot.value + delta * step)
        self._write_param(slot, value, (event.layer, "encoders", event.index))
        self._store(event.layer, "encoders", event.index, value)
        if event.layer == self._visible_layer:
            # force: the device has just redrawn this ring itself.
            self._led(P.ring_position(event.index, value, event.layer),
                      force=True)

    def _taper_scale(self, slot) -> float:
        """How much to slow this detent, given where the parameter sits.

        A fixed step in the normalised domain is not a fixed step in what the
        parameter actually means. ReaEQ's frequency is exponential with an
        offset: measured, one detent moves 14% at 20 Hz but 4% at 1 kHz, so a
        sweep tears through the bottom octaves and crawls at the top.

        "log" slows the detent near the minimum, which evens out a frequency
        control; "exp" slows it near the maximum. This is a feel adjustment
        rather than an exact linearisation -- the daemon only ever sees a
        normalised number and has no idea what unit the plugin displays.
        """
        floor = self.feel.taper_floor
        if slot.taper == "log":
            return floor + (1.0 - floor) * _clamp(slot.value) ** 0.5
        if slot.taper == "exp":
            return floor + (1.0 - floor) * (1.0 - _clamp(slot.value)) ** 0.5
        return 1.0

    def _on_push(self, event: EncoderPush) -> None:
        key = (event.layer, event.index)
        if event.pressed:
            self._held[key] = self.clock()
            self._turned_while_held.discard(key)
            return

        pressed_at = self._held.pop(key, None)
        turned = key in self._turned_while_held
        self._turned_while_held.discard(key)
        if pressed_at is None or turned:
            return                      # it was a fine-adjust hold
        if self.clock() - pressed_at > self.feel.reset_max_hold:
            return                      # held a long time but never turned

        slot = self.state.slot(event.layer, "encoders", event.index)
        if slot is None:
            return
        self._write_param(slot, slot.default,
                          (event.layer, "encoders", event.index))
        self._store(event.layer, "encoders", event.index, slot.default)
        if event.layer == self._visible_layer:
            self._led(P.ring_position(event.index, slot.default, event.layer),
                      force=True)

    def _on_button(self, event: ButtonPress) -> None:
        # Buttons 1-8 are per-plugin mappable; 9-16 are the reserved row.
        if event.index >= P.ENCODERS:
            self._on_reserved(event)
            return

        slot = self.state.slot(event.layer, "buttons", event.index)
        if slot is None:
            return
        key = (event.layer, event.index)
        if slot.mode == "momentary":
            value = 1.0 if event.pressed else 0.0
        else:
            if not event.pressed:
                return                  # toggles act on the press only
            value = 0.0 if self._toggles.get(key, slot.value >= 0.5) else 1.0
            self._toggles[key] = value >= 0.5

        self._write_param(slot, value, (event.layer, "buttons", event.index))
        self._store(event.layer, "buttons", event.index, value)
        if event.layer == self._visible_layer:
            self._led(P.button_led(
                event.index, LedState.ON if value >= 0.5 else LedState.OFF))

    def _on_reserved(self, event: ButtonPress) -> None:
        if not event.pressed:
            return                      # reserved buttons fire on press
        binding = self.state.reserved_at(event.index - P.ENCODERS)
        if binding is None:
            return
        # Both kinds go out as an action. "builtin" ids name one of the small
        # registered ReaScripts, which set an ExtState flag the panel polls --
        # OSC can trigger an action but cannot hand a value to a defer loop.
        self.osc.send(f"/action/{binding.id}")

    def set_fader_debug(self, fn) -> None:
        """Log every fader decision, including the ones to do nothing.

        The fader's failure mode is silence -- it declines to move the track
        and there is nothing anywhere to say why. Pickup not yet engaged, a
        relative pause, a walk-back, a shadow that disagrees with REAPER: all
        of them look identical from the outside, which is what makes this
        worth its own switch rather than folding into XTOUCHMINI_DEBUG.
        """
        self._fader_debug = fn

    def _flog(self, text: str) -> None:
        if self._fader_debug is not None:
            self._fader_debug(text)

    def _on_fader(self, event: FaderMove) -> None:
        mode = self.state.fader.mode
        self._flog(f"-- fader layer {event.layer} raw={event.raw} "
                   f"pos={event.value:.4f} mode={mode} "
                   f"shadow={self._volume:.4f} track={self.state.track.index}")
        previous, self._fader_last = self._fader_last, event.value
        if mode == ABSOLUTE:
            self._on_fader_absolute(event)
        elif mode == RELATIVE:
            self._on_fader_relative(event)
        else:
            self._on_fader_pickup(event, previous)
        # Whichever mode is live, the other two must not come back to a stale
        # idea of where the fader was.
        self._rel_raw = event.raw

    def _on_fader_absolute(self, event: FaderMove) -> None:
        """One to one: where the fader is, is where the track goes.

        No pickup, so a track change makes the selected track jump to wherever
        the fader happens to be sitting the moment it is next touched. That is
        the whole point of the mode and not a defect -- pickup is the mode for
        people who do not want it.
        """
        self._pickup_hint = 0
        self._send_volume(event.value)

    def _on_fader_pickup(self, event: FaderMove, previous: float | None) -> None:
        """Take over only once the fader has caught up with the track."""
        value = event.value
        if not self._fader_engaged:
            if abs(value - self._volume) <= self.feel.pickup_epsilon:
                self._fader_engaged = True
                self._flog(f"pickup: engaged, within {self.feel.pickup_epsilon}"
                           f" of target {self._volume:.4f}")
            elif previous is not None:
                # Crossing counts even if no sample landed inside epsilon: a
                # fast move can step straight over the target.
                if (previous - self._volume) * (value - self._volume) < 0:
                    self._fader_engaged = True
                    self._flog(f"pickup: engaged, crossed target "
                               f"{self._volume:.4f} between {previous:.4f} "
                               f"and {value:.4f}")
            if not self._fader_engaged:
                self._pickup_hint = 1 if value < self._volume else -1
                self._flog(
                    f"pickup: HELD at {value:.4f}, target {self._volume:.4f}"
                    f" ({self._volume - value:+.4f} away, move "
                    f"{'up' if self._pickup_hint > 0 else 'down'})")
                return

        self._pickup_hint = 0
        self._send_volume(value)

    # --- relative ---------------------------------------------------------

    def _in_end_zone(self, raw: int) -> bool:
        zone = self.state.fader.end_zone
        return zone > 0 and (raw < zone or raw > 127 - zone)

    def _heading_into_end(self, raw: int, direction: int) -> bool:
        """In an end zone AND still travelling towards that end.

        Running out of road is about the direction being pushed, not about
        where the fader happens to be sitting. A fader climbing out of the
        bottom zone has the whole travel ahead of it, so pausing it there
        would strand it after a single step.
        """
        zone = self.state.fader.end_zone
        if zone <= 0:
            return False
        return ((raw < zone and direction < 0)
                or (raw > 127 - zone and direction > 0))

    def _pinned_together(self, raw: int, direction: int) -> bool:
        """Fader parked at one end with the volume pinned at the same end.

        Relative mode's whole premise is that the two are independent, and
        that is what the walk-back is for. It stops being true at the stops:
        a fader held at the bottom against a track already silent is aligned
        with it, by accident but exactly. Moving off that end is then a
        meaningful relative move straight away, and there is nothing to
        recalibrate against.

        Without this, pulling a track down to silence costs a walk-back
        before it can be brought back up -- the fader is in the end zone, so
        it is paused, and the one thing the user wants to do next is the one
        thing it refuses. Same at the top.

        `raw` is where the fader WAS: "at its minimum" describes the resting
        position the move started from, not where it has arrived.
        """
        # A zone of 0 turns the pause off, but the stops still exist, so this
        # still has to answer for the last step at each end.
        zone = max(1, self.state.fader.end_zone)
        if raw < zone and direction > 0:
            return self._volume <= SYNC_EPSILON
        if raw > 127 - zone and direction < 0:
            return self._volume >= 1.0 - SYNC_EPSILON
        return False

    def _on_fader_relative(self, event: FaderMove) -> None:
        """Every move is a nudge, so the two faders are never aligned.

        Which means the physical one runs out of road while the track's has
        plenty left. The end zones are how it is given more: reaching one
        suspends output, and the walk back to the middle -- the recalibration
        move -- is swallowed rather than sent.

        A recalibration move ends when the user stops pushing it, and there
        are only two honest signals for that: the fader going still (handled
        in tick, because no message arrives to say so), or the fader turning
        round, which is the user going back to playing. The reversal itself
        counts as a real move: it is the first thing they meant since the
        walk-back began.

        State: `_rel_paused` is sitting in an end zone with output off.
        `_rel_recal` is 0, or the direction the walk-back is travelling in.
        """
        raw, now = event.raw, self.clock()
        previous, self._rel_raw = self._rel_raw, raw
        if previous is None:
            # Nothing to be relative to yet. The first message only says where
            # the fader already was, which is not a move the user made.
            self._flog(f"relative: baseline raw={raw}, no move yet")
            return
        delta = raw - previous
        if delta == 0:
            return
        direction = 1 if delta > 0 else -1
        self._rel_moved_at = now

        if self._rel_recal:
            if direction == self._rel_recal:
                # Still walking back. Overshooting into the far end zone is
                # not a failure, but there is no road left that way either:
                # park and wait to be walked back the other way.
                if self._in_end_zone(raw):
                    self._rel_recal = 0
                    self._rel_paused = True
                    self._flog(f"relative: walk-back ran into the far end "
                               f"zone at raw={raw}, parked")
                else:
                    self._flog(f"relative: walk-back, raw={raw} swallowed")
                return
            # Turned round: the walk-back is over and this move is real.
            self._flog(f"relative: walk-back ended by reversal at raw={raw}")
            self._rel_recal = 0

        elif self._rel_paused:
            if self._pinned_together(previous, direction):
                # The pause is real but pointless: the volume is already at
                # the end the fader is jammed against, so the two agree and
                # this move can be taken at face value.
                self._rel_paused = False
                self._flog(
                    f"relative: in sync at the "
                    f"{'bottom' if direction > 0 else 'top'} "
                    f"(volume {self._volume:.4f}), no walk-back needed")
                # falls through to the normal send below
            elif self._in_end_zone(raw):
                self._flog(f"relative: PAUSED, still in the end zone "
                           f"(raw={raw}, zone={self.state.fader.end_zone})")
                return          # shuffling about inside the zone; still off
            else:
                self._rel_paused = False
                self._rel_recal = direction
                self._flog(f"relative: walk-back started, direction "
                           f"{'up' if direction > 0 else 'down'}")
                return

        self._pickup_hint = 0
        step = delta * self.state.fader.sensitivity / 127.0
        before = self._volume
        self._send_volume(_clamp(self._volume + step))
        self._flog(f"relative: raw {previous}->{raw} ({delta:+d}) = "
                   f"{step:+.4f}, volume {before:.4f} -> {self._volume:.4f}")
        # Sent first, then paused: the move that carries the fader into the
        # zone is one the user made and meant, and swallowing it would lose
        # travel they can see themselves using.
        if self._heading_into_end(raw, direction):
            self._rel_paused = True
            self._flog(f"relative: entered the end zone at raw={raw}, "
                       f"output paused until it is walked back")

    @property
    def fader_paused(self) -> bool:
        """Relative mode: sitting in an end zone, or walking back out of one."""
        return self._rel_paused or bool(self._rel_recal)

    # --- shared -----------------------------------------------------------

    def _send_volume(self, value: float) -> None:
        self._volume = value
        self._volume_written_at = self.clock()
        address = self._volume_address()
        self._flog(f"SENT {address} {value:.4f}")
        self.osc.send(address, value)

    def _adopt_volume(self) -> bool:
        """Whether an outside volume reading may overwrite our shadow.

        Only relative mode has to ask. It adds its delta to the last value it
        believes, so adopting an echo of its own write that REAPER or the
        panel has not caught up with yet makes the next nudge start from the
        wrong place and step backwards. Absolute and pickup both send where
        the fader physically is, so a stale reading costs them nothing.
        """
        if self.state.fader.mode != RELATIVE:
            return True
        return (self.clock() - self._volume_written_at
                > self.feel.echo_suppress)

    def _volume_address(self) -> str:
        """Where to send the fader.

        Explicitly by track number, because the bare /track/volume goes to
        whatever REAPER last considered touched -- and selecting a track from
        another control surface does not update that, so the fader stays on the
        track you just left. state.json's selection is read from REAPER every
        frame however it was made, so it is the one that is actually right.

        Track 0 is the master, which OSC addresses differently; the bare form
        still reaches it, so leave those alone.
        """
        index = self.state.track.index
        return f"/track/{index}/volume" if index > 0 else "/track/volume"

    @property
    def pickup_hint(self) -> int:
        """0 engaged, +1 push the fader up, -1 pull it down."""
        return self._pickup_hint

    # --- inbound from REAPER ----------------------------------------------

    def on_feedback(self, messages) -> None:
        """Apply OSC feedback: keeps shadows true when the mouse moves a knob."""
        for msg in messages:
            if self._osc_debug is not None:
                self._osc_debug(f"osc {msg}")
            parts = msg.address.strip("/").split("/")
            if parts[:1] == ["track"] and parts[-1] == "volume" and msg.args:
                # Either /track/volume or /track/<N>/volume. The bank is wide,
                # so REAPER sends the numbered form for every track in it --
                # adopting all of them would seed the fader from whichever
                # track moved last.
                if len(parts) == 3 and _as_int(parts[1]) != self.state.track.index:
                    continue
                incoming = _clamp(msg.args[0])
                if not self._fader_engaged and self._adopt_volume():
                    if abs(incoming - self._volume) > 1e-9:
                        self._flog(f"shadow <- osc feedback {incoming:.4f} "
                                   f"(was {self._volume:.4f})")
                    self._volume = incoming
                continue
            # Which plugin the values that follow are about. REAPER sends
            # these first, in the same batch as the burst they describe.
            if parts == ["fx", "number", "str"] and msg.args:
                self._reaper_fx_number = _as_int(msg.args[0])
                continue
            if parts == ["fx", "name"] and msg.args:
                self._reaper_fx_name = str(msg.args[0])
                continue
            if parts == ["track", "number", "str"] and msg.args:
                self._reaper_track_number = _as_int(msg.args[0])
                continue
            # /fxparam/<K>/value  -- K is 1-based, bank 0 of the focused FX
            if len(parts) == 3 and parts[0] == "fxparam" and parts[2] == "value":
                try:
                    param = int(parts[1]) - 1
                except ValueError:
                    continue
                if not msg.args:
                    continue
                if self._feedback_is_for_focused_fx():
                    self._adopt_param(param, _clamp(msg.args[0]))
                else:
                    self._note_dropped_feedback()

    def _note_dropped_feedback(self) -> None:
        """Say once, not per parameter, that a burst was for another plugin."""
        if self._debug is None:
            return
        seen = (self._reaper_fx_number, self._reaper_track_number,
                self.state.fx.index, self.state.track.index)
        if seen == self._dropped_feedback_for:
            return
        self._dropped_feedback_for = seen
        self._debug(
            f"osc: ignoring parameter feedback -- REAPER says track "
            f"{self._reaper_track_number} fx {self._reaper_fx_number} "
            f"({self._reaper_fx_name!r}), state.json says track "
            f"{self.state.track.index} fx {_osc_fx(self.state.fx.index)} "
            f"({self.state.fx.name!r})")

    def _feedback_is_for_focused_fx(self) -> bool:
        """Is REAPER's parameter feedback about the plugin state.json describes?

        It is not, for as long as it takes the panel to catch up. Changing the
        focused plugin makes REAPER send a burst of every parameter of the new
        FX, and the panel republish state.json -- over a different path, at its
        own frame rate. Whichever lands second wins.

        A burst adopted against the previous plugin's slot layout writes one
        plugin's values into another plugin's slots. Measured on a ReaEQ ->
        TAL Reverb switch: the rings were left showing TAL's params 1, 3, 4 and
        6 in ReaEQ's slot order, and tick() then repainted that once a second
        until something else changed state.json. The lamps for the mappable
        buttons mostly survive it, because a wrong value usually falls on the
        same side of the 0.5 the lamp thresholds at -- a ring shows all
        thirteen steps of the error.

        So feedback counts only while REAPER's own account of what is focused
        agrees with the panel's. Nothing is adopted until REAPER has said what
        it is talking about, which it does on every focus change; before that
        the panel is the only authority, which costs nothing but a frame of
        latency, because state.json already carries every value it resolves.
        """
        if not self.state.active or self._reaper_fx_number is None:
            return False
        if self._reaper_fx_number != _osc_fx(self.state.fx.index):
            return False
        # Deliberately NOT compared against a track number. REAPER's
        # /track/number/str describes the device's current track, which follows
        # the SELECTION; this feedback describes the FOCUSED FX, which can be
        # on another track entirely. Gating one on the other held the rings off
        # for as long as the two differed. The FX number and name below are
        # what actually identify the plugin.
        # REAPER's feedback drops the "VST3: " kind of prefix that
        # TrackFX_GetFXName keeps, so the tail is what can be compared.
        if self._reaper_fx_name and self.state.fx.name:
            if not self.state.fx.name.endswith(self._reaper_fx_name):
                return False
        return True

    def _adopt_param(self, param: int, value: float) -> None:
        """A parameter changed in REAPER; update every slot pointing at it."""
        # 8 encoders and 8 mappable buttons per layer; buttons 9-16 are the
        # reserved row and never point at a parameter.
        for layer in ("A", "B"):
            for kind in ("encoders", "buttons"):
                for index in range(P.ENCODERS):
                    slot = self.state.slot(layer, kind, index)
                    if slot is None or slot.param != param:
                        continue
                    self._store(layer, kind, index, value)
                    if layer != self._visible_layer:
                        continue
                    if kind == "encoders":
                        self._led(P.ring_position(index, value, layer))
                    else:
                        self._led(P.button_led(
                            index, LedState.ON if value >= 0.5 else LedState.OFF))

    # --- LEDs -------------------------------------------------------------

    def _led(self, message: list[int], force: bool = False) -> None:
        """Send an LED message only if it would change something.

        `force` sends it anyway. The cache assumes a lamp only changes when we
        change it, and for one case that is false: the device redraws an
        encoder's ring from its own internal counter when the encoder is
        physically turned. A detent usually moves the parameter less than one
        of the ring's thirteen steps, so the repaint looks redundant and is
        suppressed -- leaving the device's own drawing on screen until the
        step happens to change or the next tick() repaint comes round. That is
        the ring "snapping back" while a knob is being turned.
        """
        key = (message[0], message[1])
        if not force and self._led_cache.get(key) == message[2]:
            return
        self._led_cache[key] = message[2]
        self.midi.send(message)

    def _forget_leds(self) -> None:
        """Drop the cache, so the next paint is unconditional."""
        self._led_cache.clear()

    def refresh_leds(self) -> None:
        """Repaint the whole surface for the visible layer."""
        layer = self._visible_layer
        for i in range(P.ENCODERS):
            slot = self.state.slot(layer, "encoders", i)
            self._led(P.ring_position(
                i, None if slot is None else slot.value, layer))
        for i in range(P.ENCODERS):
            slot = self.state.slot(layer, "buttons", i)
            self._led(P.button_led(i, self._button_state(slot)))
        for i in range(P.ENCODERS, P.BUTTONS):
            bound = self.state.reserved_at(i - P.ENCODERS) is not None
            self._led(P.button_led(i, LedState.ON if bound else LedState.OFF))

    @staticmethod
    def _button_state(slot) -> LedState:
        """A mapped button's lamp follows its parameter: lit when engaged."""
        if slot is None:
            return LedState.OFF
        return LedState.ON if slot.value >= 0.5 else LedState.OFF

    def tick(self) -> None:
        """Periodic upkeep: put the lamps back if something cleared them.

        Called from the poll loop. Cheap enough to run every iteration -- it
        does nothing until the interval has elapsed.
        """
        now = self.clock()
        # Before the LED early-return, not after: a walk-back ends by going
        # still, and nothing arrives to announce that. If this only ran when
        # the lamps were due, the timeout would be the repaint interval
        # rounded up rather than the one the panel asked for.
        if self._rel_recal and not self._in_end_zone(self._rel_raw or 0):
            if now - self._rel_moved_at >= self.state.fader.recal_timeout:
                self._rel_recal = 0
        if now - self._last_repaint < self.feel.led_refresh_interval:
            return
        self._last_repaint = now
        self._forget_leds()
        self.refresh_leds()

    def set_visible_layer(self, layer: str) -> None:
        if layer != self._visible_layer:
            self._visible_layer = layer
            # Every lamp may legitimately need to change, including back to a
            # value the cache still believes is on the device.
            self._forget_leds()
            self.refresh_leds()

    def all_off(self) -> None:
        # Both layers' rings, not just the visible one: they are addressed by
        # different CCs and the device remembers each layer's lamps, so
        # clearing only the visible layer leaves the other one lit the next
        # time the layer button is pressed.
        self._forget_leds()
        for layer in (P.LAYER_A, P.LAYER_B):
            for i in range(P.ENCODERS):
                self.midi.send(P.ring_all(i, False, layer))
        for i in range(P.BUTTONS):
            self.midi.send(P.button_led(i, LedState.OFF))
        self._forget_leds()

    # --- helpers ----------------------------------------------------------

    def _write_param(self, slot, value: float, key=None) -> None:
        if key is not None:
            self._written_at[key] = self.clock()
        self.osc.send(
            f"/track/{self._focused_track()}"
            f"/fx/{_osc_fx(self.state.fx.index)}"
            f"/fxparam/{_osc_param(slot.param)}/value",
            value)

    def _focused_track(self) -> int:
        """The track the focused plugin is on -- NOT the selected one.

        state.json carries both, and they are different things: track.index is
        the selection, which the fader follows on purpose, while fx.track is
        wherever the plugin you are looking at happens to live. Addressing a
        write with the selection means that selecting any other track sends
        every encoder to the plugin at the same chain position over there --
        which is nothing at all if that track is shorter, and the wrong plugin
        if it is not. It works only while the two happen to coincide.

        Falls back to the selection when a panel too old to publish fx.track
        is running, which is what the daemon did everywhere before.
        """
        return (self.state.fx.track if self.state.fx.track >= 0
                else self.state.track.index)

    def _store(self, layer, kind, index, value) -> None:
        try:
            current = self.state.layers[layer][kind][index]
            if current is not None:
                self.state.layers[layer][kind][index] = replace(
                    current, value=_clamp(value))
        except (KeyError, IndexError):
            pass


def _as_int(value) -> int | None:
    """REAPER's /str feedback is a string. Anything unparseable means unknown."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _clamp(value, low=0.0, high=1.0) -> float:
    try:
        return min(high, max(low, float(value)))
    except (TypeError, ValueError):
        return low

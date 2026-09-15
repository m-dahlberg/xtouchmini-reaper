import json
import unittest

from xtouchmini.engine import Engine, Feel
from xtouchmini.protocol import (ButtonPress, EncoderPush, EncoderTurn,
                                 FaderMove, LedState)
from xtouchmini.protocol import ring_position, button_led
from xtouchmini import protocol as P
from xtouchmini.osc import Message
from xtouchmini.state import parse


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


class Recorder:
    def __init__(self): self.msgs = []
    def send(self, *args): self.msgs.append(args[0] if len(args) == 1 else args)
    def clear(self): self.msgs.clear()


def state(active=True, track=3, volume=0.5, encoders=None, buttons=None,
          layer_b=None, reserved=None, fx_track=None, fader=None):
    enc = encoders if encoders is not None else [
        {"param": 12, "name": "Thr", "value": 0.5, "default": 0.25}]
    return parse(json.dumps({
        "version": 1, "seq": 1, "active": active,
        "fader": fader or {},
        "track": {"index": track, "name": "Vox", "volume": volume},
        "fx": {"index": 0, "ident": "ReaComp", "name": "ReaComp",
               "track": track if fx_track is None else fx_track},
        "layers": {
            "A": {"encoders": enc, "buttons": buttons or []},
            "B": {"encoders": layer_b or [], "buttons": []},
        },
        "reserved": reserved or [],
    }))


class Base(unittest.TestCase):
    def setUp(self):
        self.osc, self.midi, self.clock = Recorder(), Recorder(), FakeClock()
        self.engine = Engine(self.osc, self.midi, Feel(), self.clock)
        self.engine.set_state(state())
        self.osc.clear(); self.midi.clear()


class TestEncoders(Base):
    def test_turn_writes_the_resolved_address(self):
        # The track number is REAPER's ABSOLUTE one, straight out of
        # state.json. /track/<N>/ is an index into the OSC track bank, so the
        # pattern file has to declare a bank wide enough to contain it --
        # DEVICE_TRACK_COUNT 128. With a bank of 1 the only reachable slot is
        # 1, which follows the SELECTED track: encoders then die whenever the
        # focused plugin is not on the selected track, or worse, drive the
        # same chain position on whichever track is selected.
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        addr, value = self.osc.msgs[0]
        self.assertEqual(addr, "/track/3/fx/1/fxparam/13/value")
        self.assertAlmostEqual(value, 0.5 + 1 / 128)

    def test_hardware_acceleration_is_not_doubled(self):
        # The device sends 3 for a fast detent and 1 for a slow one; the engine
        # must scale linearly and add no curve of its own.
        self.engine.handle(EncoderTurn(0, "A", delta=3))
        one = self.osc.msgs[0][1] - 0.5
        self.assertAlmostEqual(one, 3 / 128)

    def test_unassigned_encoder_does_nothing(self):
        self.engine.handle(EncoderTurn(5, "A", delta=1))
        self.assertEqual(self.osc.msgs, [])

    def test_inactive_state_makes_encoders_dead(self):
        # The strict-focus rule: no plugin focused, no parameter moves.
        self.engine.set_state(state(active=False))
        self.osc.clear()
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        self.assertEqual(self.osc.msgs, [])

    def test_value_is_clamped(self):
        self.engine.handle(EncoderTurn(0, "A", delta=10_000))
        self.assertEqual(self.osc.msgs[-1][1], 1.0)
        self.engine.handle(EncoderTurn(0, "A", delta=-10_000))
        self.assertEqual(self.osc.msgs[-1][1], 0.0)

    def test_successive_turns_accumulate(self):
        for _ in range(4):
            self.engine.handle(EncoderTurn(0, "A", delta=1))
        self.assertAlmostEqual(self.osc.msgs[-1][1], 0.5 + 4 / 128)

    def test_ring_follows_the_value(self):
        # The ring has 13 LEDs, so it takes about 1/12 of the range to move.
        self.engine.handle(EncoderTurn(0, "A", delta=16))
        self.assertIn(ring_position(0, 0.5 + 16 / 128, "A"), self.midi.msgs)


class TestAbsoluteEncoders(Base):
    def test_first_message_is_swallowed_then_deltas_follow(self):
        # An absolute encoder's first report after a jump would otherwise be
        # read as a delta the size of the jump.
        self.engine.handle(EncoderTurn(0, "A", absolute=40))
        self.assertEqual(self.osc.msgs, [])
        self.engine.handle(EncoderTurn(0, "A", absolute=43))
        self.assertAlmostEqual(self.osc.msgs[0][1], 0.5 + 3 / 128)

    def test_no_movement_sends_nothing(self):
        self.engine.handle(EncoderTurn(0, "A", absolute=40))
        self.engine.handle(EncoderTurn(0, "A", absolute=40))
        self.assertEqual(self.osc.msgs, [])


class TestPushGesture(Base):
    def test_push_and_release_resets_to_default(self):
        self.engine.handle(EncoderPush(0, True, "A"))
        self.clock.advance(0.1)
        self.engine.handle(EncoderPush(0, False, "A"))
        self.assertAlmostEqual(self.osc.msgs[-1][1], 0.25)

    def test_push_and_turn_is_fine_adjust_not_a_reset(self):
        self.engine.handle(EncoderPush(0, True, "A"))
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        fine = self.osc.msgs[-1][1]
        self.assertAlmostEqual(fine, 0.5 + 1 / 1024)
        self.osc.clear()
        self.engine.handle(EncoderPush(0, False, "A"))
        self.assertEqual(self.osc.msgs, [], "a fine-adjust must not then reset")

    def test_long_hold_without_turning_does_not_reset(self):
        # Resting a finger on the encoder should not wipe the value.
        self.engine.handle(EncoderPush(0, True, "A"))
        self.clock.advance(5.0)
        self.engine.handle(EncoderPush(0, False, "A"))
        self.assertEqual(self.osc.msgs, [])

    def test_fine_mode_only_applies_to_the_held_encoder(self):
        self.engine.set_state(state(encoders=[
            {"param": 1, "name": "a", "value": 0.5, "default": 0.0},
            {"param": 2, "name": "b", "value": 0.5, "default": 0.0}]))
        self.osc.clear()
        self.engine.handle(EncoderPush(0, True, "A"))
        self.engine.handle(EncoderTurn(1, "A", delta=1))
        self.assertAlmostEqual(self.osc.msgs[-1][1], 0.5 + 1 / 128)


class TestButtons(Base):
    def toggle_state(self):
        return state(buttons=[{"param": 5, "name": "Byp", "value": 0.0,
                               "mode": "toggle"}])

    def momentary_state(self):
        return state(buttons=[{"param": 5, "name": "Byp", "value": 0.0,
                               "mode": "momentary"}])

    def test_toggle_flips_on_press_only(self):
        self.engine.set_state(self.toggle_state()); self.osc.clear()
        self.engine.handle(ButtonPress(0, True, "A"))
        self.assertEqual(self.osc.msgs[-1][1], 1.0)
        self.engine.handle(ButtonPress(0, False, "A"))
        self.assertEqual(len(self.osc.msgs), 1, "release must not act")
        self.engine.handle(ButtonPress(0, True, "A"))
        self.assertEqual(self.osc.msgs[-1][1], 0.0)

    def test_momentary_follows_the_press(self):
        self.engine.set_state(self.momentary_state()); self.osc.clear()
        self.engine.handle(ButtonPress(0, True, "A"))
        self.assertEqual(self.osc.msgs[-1][1], 1.0)
        self.engine.handle(ButtonPress(0, False, "A"))
        self.assertEqual(self.osc.msgs[-1][1], 0.0)

    def test_button_led_tracks_state(self):
        self.engine.set_state(self.toggle_state()); self.midi.clear()
        self.engine.handle(ButtonPress(0, True, "A"))
        self.assertIn(button_led(0, LedState.ON), self.midi.msgs)

    def test_unassigned_button_does_nothing(self):
        self.engine.handle(ButtonPress(3, True, "A"))
        self.assertEqual(self.osc.msgs, [])


class TestReservedRow(Base):
    def reserved_state(self):
        return state(reserved=[{"kind": "builtin", "id": "_RSbank"},
                               {"kind": "action", "id": "_RS42"}])

    def test_reserved_button_fires_an_action(self):
        self.engine.set_state(self.reserved_state()); self.osc.clear()
        self.engine.handle(ButtonPress(8, True, "A"))     # button 9
        self.assertEqual(self.osc.msgs, ["/action/_RSbank"])

    def test_reserved_fires_on_press_not_release(self):
        self.engine.set_state(self.reserved_state()); self.osc.clear()
        self.engine.handle(ButtonPress(8, False, "A"))
        self.assertEqual(self.osc.msgs, [])

    def test_reserved_still_works_with_no_plugin_focused(self):
        # The reserved row is global; strict focus must not disable it.
        s = self.reserved_state()
        inactive = parse(json.dumps({
            "version": 1, "seq": 2, "active": False,
            "track": {"index": 3, "name": "V", "volume": 0.5},
            "fx": {"index": -1, "ident": "", "name": ""},
            "layers": {}, "reserved": [{"kind": "builtin", "id": "_RSbank"}]}))
        self.engine.set_state(inactive); self.osc.clear()
        self.engine.handle(ButtonPress(8, True, "A"))
        self.assertEqual(self.osc.msgs, ["/action/_RSbank"])

    def test_unbound_reserved_button_does_nothing(self):
        self.engine.handle(ButtonPress(15, True, "A"))
        self.assertEqual(self.osc.msgs, [])


class TestFaderPickup(Base):
    """The fader is not motorised, so it must catch up before it takes over."""

    def move(self, *values):
        for v in values:
            self.engine.handle(FaderMove(v, int(v * 127)))

    def test_does_not_jump_the_volume(self):
        self.move(0.1)
        self.assertEqual(self.osc.msgs, [])
        self.assertEqual(self.engine.pickup_hint, 1, "hint should say move up")

    def test_engages_on_crossing_from_below(self):
        self.move(0.1, 0.3)
        self.assertEqual(self.osc.msgs, [])
        self.move(0.6)
        self.assertEqual(self.osc.msgs[-1], ("/track/3/volume", 0.6))

    def test_engages_on_crossing_from_above(self):
        self.move(0.9, 0.7)
        self.assertEqual(self.osc.msgs, [])
        self.assertEqual(self.engine.pickup_hint, -1)
        self.move(0.4)
        self.assertEqual(self.osc.msgs[-1], ("/track/3/volume", 0.4))

    def test_engages_when_arriving_within_epsilon(self):
        self.move(0.49)
        self.assertEqual(self.osc.msgs[-1], ("/track/3/volume", 0.49))

    def test_stays_engaged_afterwards(self):
        self.move(0.49, 0.2, 0.8)
        self.assertEqual(self.osc.msgs[-1], ("/track/3/volume", 0.8))
        self.assertEqual(self.engine.pickup_hint, 0)

    def test_changing_track_disengages(self):
        self.move(0.49, 0.8)
        self.osc.clear()
        self.engine.set_state(state(track=4, volume=0.2))
        self.osc.clear()
        self.move(0.85)
        self.assertEqual(self.osc.msgs, [], "must pick up again on a new track")

    def test_same_track_update_does_not_disengage(self):
        self.move(0.49)
        self.osc.clear()
        self.engine.set_state(state(track=3, volume=0.49))
        self.osc.clear()
        self.move(0.7)
        self.assertEqual(self.osc.msgs[-1], ("/track/3/volume", 0.7))


class TestTaper(Base):
    """A fixed step in the normalised domain is not a fixed step in what the
    parameter means. Measured on ReaEQ's frequency: one detent moves 14% at
    20 Hz but 4% at 1 kHz."""

    def slot(self, taper, value, sens=1.0):
        return state(encoders=[{"param": 12, "name": "Freq", "value": value,
                                "default": 0.5, "taper": taper,
                                "sensitivity": sens}])

    def step_at(self, taper, value, sens=1.0):
        """Size of one detent at `value`, turned in whichever direction has
        room -- at 0.0 or 1.0 the clamp would otherwise hide the real step."""
        # Past the echo-suppression window, or the engine keeps the value from
        # the previous case instead of adopting this one.
        self.clock.advance(1.0)
        self.engine.set_state(self.slot(taper, value, sens))
        self.osc.clear()
        delta = -1 if value > 0.5 else 1
        self.engine.handle(EncoderTurn(0, "A", delta=delta))
        return abs(self.osc.msgs[-1][1] - value)

    def test_linear_is_the_default_and_is_unchanged(self):
        self.assertAlmostEqual(self.step_at("linear", 0.1), 1 / 128)
        self.assertAlmostEqual(self.step_at("linear", 0.9), 1 / 128)

    def test_log_moves_less_near_the_minimum(self):
        low = self.step_at("log", 0.02)
        high = self.step_at("log", 0.9)
        self.assertLess(low, high)
        self.assertLess(low, 1 / 128, "must be finer than linear at the bottom")

    def test_log_reaches_full_speed_at_the_top(self):
        self.assertAlmostEqual(self.step_at("log", 1.0), 1 / 128, places=5)

    def test_exp_is_the_mirror_of_log(self):
        self.assertAlmostEqual(self.step_at("exp", 0.0),
                               self.step_at("log", 1.0), places=6)
        self.assertLess(self.step_at("exp", 0.98), self.step_at("exp", 0.1))

    def test_taper_never_stalls_the_encoder(self):
        # A curve that reached zero would leave the control stuck at the end
        # of its travel with no way back.
        for taper in ("log", "exp"):
            for value in (0.0, 0.5, 1.0):
                self.assertGreater(self.step_at(taper, value), 0.0,
                                   f"{taper} at {value}")

    def test_sensitivity_scales_the_step(self):
        self.assertAlmostEqual(self.step_at("linear", 0.5, sens=2.0), 2 / 128)
        self.assertAlmostEqual(self.step_at("linear", 0.5, sens=0.5), 0.5 / 128)

    def test_sensitivity_combines_with_taper(self):
        plain = self.step_at("log", 0.25)
        doubled = self.step_at("log", 0.25, sens=2.0)
        self.assertAlmostEqual(doubled, plain * 2)

    def test_unknown_taper_falls_back_to_linear(self):
        self.assertAlmostEqual(self.step_at("nonsense", 0.5), 1 / 128)

    def test_absurd_sensitivity_is_clamped(self):
        for bad in (0.0, -5, 1e9, "nonsense", None):
            moved = self.step_at("linear", 0.5, sens=bad)
            self.assertGreater(moved, 0.0, f"sensitivity {bad!r}")
            self.assertLessEqual(moved, 8 / 128, f"sensitivity {bad!r}")


class TestLeds(Base):
    """The lamps are the only feedback the hardware can give, and the panel
    republishes state.json on every value change -- so a naive repaint sends
    24 messages a frame."""

    def toggle_state(self, value=0.0):
        return state(buttons=[{"param": 5, "name": "Byp", "value": value,
                               "mode": "toggle"}])

    def test_button_lamp_follows_a_toggle(self):
        self.engine.set_state(self.toggle_state()); self.midi.clear()
        self.engine.handle(ButtonPress(0, True, "A"))
        self.assertIn(button_led(0, LedState.ON), self.midi.msgs)
        self.midi.clear()
        self.engine.handle(ButtonPress(0, True, "A"))
        self.assertIn(button_led(0, LedState.OFF), self.midi.msgs)

    def test_lamp_reflects_a_value_changed_in_reaper(self):
        # Engaged with the mouse rather than the button: the lamp must follow.
        self.engine.set_state(self.toggle_state(value=0.0))
        self.midi.clear()
        self.clock.advance(1.0)
        self.engine.set_state(self.toggle_state(value=1.0))
        self.assertIn(button_led(0, LedState.ON), self.midi.msgs)

    def test_a_turn_always_repaints_its_own_ring(self):
        """The device redraws the ring it is being turned, so our cached
        belief about that lamp is stale exactly when it matters -- and a move
        too small to change the wire value must still be sent, or the device's
        own drawing is what stays on screen.

        A fine turn is the sub-resolution case: 1/1024 of the range is about a
        tenth of one step of the 0-127 ring.
        """
        self.engine.handle(EncoderPush(0, True, "A"))      # hold = fine
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        first = [m for m in self.midi.msgs if m[1] == P.ring_cc(0, "A")]
        self.midi.clear()
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        again = [m for m in self.midi.msgs if m[1] == P.ring_cc(0, "A")]
        self.assertTrue(first and again, "the repaint must go out regardless")
        self.assertEqual(first[-1][2], again[-1][2],
                         "this move is too small to change the ring value -- "
                         "which is the case that used to be suppressed")

    def test_unmapped_buttons_are_dark(self):
        self.engine.set_state(state()); self.midi.clear()
        self.engine._forget_leds()
        self.engine.refresh_leds()
        for i in range(1, 8):
            self.assertIn(button_led(i, LedState.OFF), self.midi.msgs)

    def test_bound_reserved_buttons_are_lit(self):
        self.engine.set_state(state(
            reserved=[{"kind": "builtin", "id": "_RSx"}]))
        self.midi.clear()
        self.engine._forget_leds()
        self.engine.refresh_leds()
        self.assertIn(button_led(8, LedState.ON), self.midi.msgs)
        self.assertIn(button_led(9, LedState.OFF), self.midi.msgs)

    def test_repaint_sends_nothing_when_nothing_changed(self):
        # The flood this guards against: one knob turn republishes state.json
        # every frame, and each republish repainted the whole surface.
        self.engine.refresh_leds()
        self.midi.clear()
        self.engine.refresh_leds()
        self.assertEqual(self.midi.msgs, [])

    def test_a_layer_switch_repaints_unconditionally(self):
        # The cache describes the lamps as they are, and a layer switch can
        # legitimately need to set one back to what the cache already holds.
        self.engine.refresh_leds()
        self.midi.clear()
        self.engine.set_visible_layer("B")
        self.assertTrue(self.midi.msgs, "must repaint on a layer switch")

    def test_tick_repaints_after_the_interval(self):
        # The layer button sends no MIDI and the device keeps LED state per
        # layer, so a switch silently clears every lamp. Nothing can detect
        # that; only an unconditional repaint puts them back.
        self.engine.refresh_leds()
        self.midi.clear()
        self.engine.tick()
        self.assertEqual(self.midi.msgs, [], "too soon to repaint")
        self.clock.advance(1.5)
        self.engine.tick()
        self.assertTrue(self.midi.msgs, "must repaint once the interval passes")

    def test_tick_is_cheap_between_repaints(self):
        self.clock.advance(2.0)
        self.engine.tick()
        self.midi.clear()
        for _ in range(500):
            self.engine.tick()
        self.assertEqual(self.midi.msgs, [], "no work between intervals")

    def test_repaint_rate_is_bounded(self):
        # 24 lamps per repaint at a one-second interval, against the ~700 a
        # second the un-deduplicated version sent.
        sent = 0
        for _ in range(10):
            self.clock.advance(1.1)
            self.midi.clear()
            self.engine.tick()
            sent += len(self.midi.msgs)
        self.assertLessEqual(sent, 10 * (P.ENCODERS + P.BUTTONS))

    def test_all_off_always_reaches_the_device(self):
        self.engine.refresh_leds()
        self.midi.clear()
        self.engine.all_off()
        self.assertTrue(self.midi.msgs)
        for i in range(P.BUTTONS):
            self.assertIn(button_led(i, LedState.OFF), self.midi.msgs)


class TestEchoSuppression(Base):
    """The panel republishes state.json on every value change, so a turn can
    race its own echo. Adopting the lagging value makes the next detent start
    from the wrong place, which reads as an encoder moving both ways at once."""

    def test_stale_echo_does_not_rewind_the_value(self):
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        after = self.osc.msgs[-1][1]
        self.assertAlmostEqual(after, 0.5 + 1 / 128)

        # The panel is still reporting the pre-turn value.
        self.engine.set_state(state())
        self.osc.clear()
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        self.assertAlmostEqual(self.osc.msgs[-1][1], 0.5 + 2 / 128,
                               msg="second detent must continue, not rewind")

    def test_echo_is_adopted_once_the_window_passes(self):
        # Outside the window the panel is the authority again -- that is how a
        # mouse move in the plugin reaches the controller.
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        self.clock.advance(5.0)
        self.engine.set_state(state(encoders=[
            {"param": 12, "name": "Thr", "value": 0.9, "default": 0.25}]))
        self.osc.clear()
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        self.assertAlmostEqual(self.osc.msgs[-1][1], 0.9 + 1 / 128)

    def test_a_remap_wins_even_inside_the_window(self):
        # Different parameter in the slot: their value is the authority.
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        self.engine.set_state(state(encoders=[
            {"param": 99, "name": "Other", "value": 0.2, "default": 0.0}]))
        self.osc.clear()
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        self.assertAlmostEqual(self.osc.msgs[-1][1], 0.2 + 1 / 128)

    def test_buttons_are_suppressed_too(self):
        s = state(buttons=[{"param": 5, "name": "B", "value": 0.0,
                            "mode": "toggle"}])
        self.engine.set_state(s)
        self.osc.clear()
        self.engine.handle(ButtonPress(0, True, "A"))
        self.assertEqual(self.osc.msgs[-1][1], 1.0)
        self.engine.set_state(state(buttons=[
            {"param": 5, "name": "B", "value": 0.0, "mode": "toggle"}]))
        self.osc.clear()
        self.engine.handle(ButtonPress(0, True, "A"))
        self.assertEqual(self.osc.msgs[-1][1], 0.0, "must toggle off, not on")


class TestFaderAbsolute(Base):
    """One to one: no pickup, no memory, the track goes where the fader is."""

    def setUp(self):
        super().setUp()
        self.engine.set_state(state(fader={"mode": "absolute"}))
        self.osc.clear()

    def move(self, *raws):
        for raw in raws:
            self.engine.handle(FaderMove(raw / 127.0, raw))

    def test_the_first_move_takes_over_immediately(self):
        # The pickup mode's whole job is NOT to do this, so it is the one
        # thing worth asserting: no catching up, no epsilon, just the value.
        self.move(13)
        self.assertEqual(self.osc.msgs, [("/track/3/volume", 13 / 127.0)])

    def test_every_move_is_sent(self):
        self.move(13, 100, 4)
        self.assertEqual([v for _, v in self.osc.msgs],
                         [13 / 127.0, 100 / 127.0, 4 / 127.0])

    def test_a_track_change_does_not_make_it_pick_up(self):
        self.move(100)
        self.osc.clear()
        self.engine.set_state(state(track=4, volume=0.1,
                                    fader={"mode": "absolute"}))
        self.osc.clear()
        self.move(101)
        self.assertEqual(self.osc.msgs, [("/track/4/volume", 101 / 127.0)])


class TestFaderRelative(Base):
    """Every move is a nudge; the end zones buy back the travel it costs."""

    def setUp(self):
        super().setUp()
        self.set_mode()
        self.osc.clear()

    def set_mode(self, **kw):
        fader = {"mode": "relative"}
        fader.update(kw)
        self.engine.set_state(state(volume=self.engine._volume, fader=fader))

    def move(self, *raws):
        for raw in raws:
            self.engine.handle(FaderMove(raw / 127.0, raw))

    def values(self):
        return [round(v, 6) for _, v in self.osc.msgs]

    # --- the plain case ---------------------------------------------------

    def test_the_first_message_is_a_position_not_a_move(self):
        # It only says where the fader already was. Treating it as a move
        # would make every mode switch nudge the volume.
        self.move(60)
        self.assertEqual(self.osc.msgs, [])

    def test_a_move_applies_its_delta_to_the_track(self):
        self.move(60, 70)
        self.assertEqual(self.values(), [round(0.5 + 10 / 127.0, 6)])

    def test_the_fader_and_the_track_never_realign(self):
        # Raw 70 is 0.55 as an absolute position; relative mode must send the
        # accumulated volume instead, which is what makes the two independent.
        self.move(60, 70)
        self.assertNotAlmostEqual(self.osc.msgs[-1][1], 70 / 127.0)

    def test_sensitivity_scales_the_delta(self):
        self.set_mode(sensitivity=0.5)
        self.osc.clear()
        self.move(60, 70)
        self.assertEqual(self.values(), [round(0.5 + 10 / 127.0 * 0.5, 6)])

    def test_it_clamps_rather_than_wrapping(self):
        self.engine.set_state(state(volume=0.98, fader={"mode": "relative"}))
        self.osc.clear()
        self.move(60, 120)
        self.assertEqual(self.osc.msgs[-1][1], 1.0)

    def test_a_still_fader_sends_nothing(self):
        self.move(60, 60, 60)
        self.assertEqual(self.osc.msgs, [])

    # --- end zones --------------------------------------------------------

    def test_the_move_into_the_end_zone_is_sent(self):
        self.move(120, 126)
        self.assertEqual(len(self.osc.msgs), 1, "the entering move is real")

    def test_output_stops_once_inside_the_end_zone(self):
        self.move(120, 126)
        self.osc.clear()
        self.move(127, 125, 126)
        self.assertEqual(self.osc.msgs, [], "no road left; nothing goes out")

    def test_the_bottom_end_zone_works_the_same_way(self):
        self.move(10, 1)
        self.osc.clear()
        self.move(0, 2)
        self.assertEqual(self.osc.msgs, [])

    def test_end_zone_width_is_configurable(self):
        self.set_mode(end_zone=10)
        self.osc.clear()
        self.move(100, 120)
        self.osc.clear()
        self.move(122)
        self.assertEqual(self.osc.msgs, [], "raw 120 is inside a zone of 10")

    def test_a_zero_width_end_zone_never_pauses(self):
        self.set_mode(end_zone=0)
        self.osc.clear()
        self.move(100, 127)
        self.osc.clear()
        self.move(126, 127)
        self.assertEqual(len(self.osc.msgs), 2)

    # --- the recalibration move -------------------------------------------

    def test_the_walk_back_out_of_a_zone_is_swallowed(self):
        self.move(120, 126)
        self.osc.clear()
        self.move(100, 80, 60)
        self.assertEqual(self.osc.msgs, [],
                         "the walk-back must not move the track")

    def test_reversing_ends_the_walk_back_and_that_move_counts(self):
        self.move(120, 126)
        self.osc.clear()
        volume = self.engine._volume
        self.move(60)              # walking back down
        self.move(65)              # turned round: this is a real move again
        self.assertEqual(self.values(), [round(volume + 5 / 127.0, 6)])

    def test_going_still_ends_the_walk_back(self):
        self.move(120, 126)
        self.osc.clear()
        self.move(60)
        self.clock.advance(1.5)
        self.engine.tick()
        volume = self.engine._volume
        self.move(50)              # still going down, but it is a move now
        self.assertEqual(self.values(), [round(volume - 10 / 127.0, 6)])

    def test_going_still_for_less_than_the_timeout_does_not_end_it(self):
        self.move(120, 126)
        self.osc.clear()
        self.move(60)
        self.clock.advance(0.5)
        self.engine.tick()
        self.move(50)
        self.assertEqual(self.osc.msgs, [], "still walking back")

    def test_the_timeout_is_configurable(self):
        self.set_mode(recal_timeout=3.0)
        self.osc.clear()
        self.move(120, 126)
        self.move(60)
        self.clock.advance(1.5)
        self.engine.tick()
        self.osc.clear()
        self.move(50)
        self.assertEqual(self.osc.msgs, [], "1.5s is short of a 3s timeout")

    def test_walking_back_into_the_far_end_zone_parks_there(self):
        # Overshooting the middle is not an error, but there is no road that
        # way either: it has to pause again rather than start sending.
        self.move(120, 126)
        self.osc.clear()
        self.move(60, 1)           # walked all the way to the bottom zone
        self.assertEqual(self.osc.msgs, [])
        self.move(2, 0)            # shuffling inside the bottom zone
        self.assertEqual(self.osc.msgs, [])
        self.move(60)              # a fresh walk-back, upwards this time
        self.assertEqual(self.osc.msgs, [])
        self.move(50)              # reversed: real again
        self.assertEqual(len(self.osc.msgs), 1)

    def test_the_zone_does_not_re_arm_without_leaving_it(self):
        self.move(120, 126)
        self.osc.clear()
        self.move(100)             # walk-back starts
        self.clock.advance(1.5)
        self.engine.tick()         # and ends
        self.move(110, 126)        # back up into the zone: sends, then pauses
        self.assertEqual(len(self.osc.msgs), 2)
        self.osc.clear()
        self.move(127)
        self.assertEqual(self.osc.msgs, [])

    def test_fader_paused_reports_the_state_for_the_panel(self):
        self.assertFalse(self.engine.fader_paused)
        self.move(120, 126)
        self.assertTrue(self.engine.fader_paused, "parked in the zone")
        self.move(100)
        self.assertTrue(self.engine.fader_paused, "walking back")
        self.clock.advance(1.5)
        self.engine.tick()
        self.assertFalse(self.engine.fader_paused)

    # --- pinned at an end together ----------------------------------------
    #
    # Relative mode's premise is that the fader and the track are independent.
    # That stops being true at the stops: a fader held at the bottom against a
    # track already silent is aligned with it exactly, so moving off that end
    # means something straight away and there is nothing to recalibrate.

    def test_coming_back_up_from_silence_needs_no_walk_back(self):
        # The case that made this necessary: pull a track to silence, then
        # want it back. Without the rule the fader is parked in the end zone,
        # so the one thing you want to do next is the one thing it refuses.
        self.engine.set_state(state(volume=0.05, fader={"mode": "relative"}))
        self.osc.clear()
        self.move(20, 1)                    # drags the volume to 0 and parks
        self.assertEqual(self.engine._volume, 0.0, "volume bottomed out")
        self.assertTrue(self.engine.fader_paused, "and the fader is paused")
        self.osc.clear()
        self.move(6)                        # straight back up
        self.assertEqual(self.values(), [round(5 / 127.0, 6)],
                         "the move up is sent, not swallowed")
        self.assertFalse(self.engine.fader_paused, "and the pause is lifted")

    def test_coming_back_down_from_the_top_needs_no_walk_back(self):
        self.engine.set_state(state(volume=0.95, fader={"mode": "relative"}))
        self.osc.clear()
        self.move(107, 126)                 # drives the volume to 1.0 and parks
        self.assertEqual(self.engine._volume, 1.0)
        self.assertTrue(self.engine.fader_paused)
        self.osc.clear()
        self.move(121)
        self.assertEqual(self.values(), [round(1.0 - 5 / 127.0, 6)])
        self.assertFalse(self.engine.fader_paused)

    def test_climbing_out_of_the_zone_does_not_re_arm_the_pause(self):
        # The first step up lifts the volume off the end, so from the second
        # step on the two are no longer "in sync" -- and the fader is still
        # inside the zone. Arming on position alone would therefore strand it
        # again immediately, one step into the climb. A wide zone makes that
        # visible: with the default 3 the climb leaves the zone before the
        # difference shows.
        self.set_mode(end_zone=10)
        self.engine.set_state(state(volume=0.05, fader={"mode": "relative",
                                                        "end_zone": 10}))
        self.osc.clear()
        self.move(20, 1)                    # bottoms out and parks
        self.assertEqual(self.engine._volume, 0.0)
        self.assertTrue(self.engine.fader_paused)
        self.osc.clear()
        self.move(3, 5, 7, 9)               # every one of these is still
        self.assertEqual(len(self.osc.msgs), 4,                # inside the zone
                         "every step of the climb is sent")
        self.assertFalse(self.engine.fader_paused)

    def test_a_zone_the_volume_is_not_pinned_in_still_needs_a_walk_back(self):
        # The regression guard: this rule must not swallow the ordinary case.
        self.move(20, 1)                    # volume 0.5 -> still mid-range
        self.assertGreater(self.engine._volume, 0.1, "nowhere near silent")
        self.osc.clear()
        self.move(40, 60)
        self.assertEqual(self.osc.msgs, [], "still a walk-back")

    def test_pushing_further_into_the_end_is_not_a_sync_move(self):
        # At the bottom with the volume at zero, DOWN is not the direction
        # the rule is about -- only coming back off the end counts.
        self.engine.set_state(state(volume=0.05, fader={"mode": "relative"}))
        self.move(20, 2)
        self.osc.clear()
        self.move(1, 0)
        self.assertEqual(self.osc.msgs, [], "still paused going further down")

    def test_within_one_step_of_the_end_counts_as_at_it(self):
        # A volume left a hair above silence is silent as far as anything the
        # user can do about it is concerned, and demanding a walk-back for it
        # would make the rule fire unpredictably.
        # Chosen so the 59-step drop lands half a step above silence.
        self.engine.set_state(state(volume=59.5 / 127.0,
                                    fader={"mode": "relative"}))
        self.osc.clear()
        self.move(60, 1)                    # lands just above zero, in the zone
        volume = self.engine._volume
        self.assertGreater(volume, 0.0, "not exactly zero")
        self.assertLess(volume, 1 / 127.0, "but within one step of it")
        self.osc.clear()
        self.move(6)
        self.assertEqual(len(self.osc.msgs), 1, "treated as pinned together")

    def test_a_volume_clearly_off_the_end_is_not_pinned(self):
        self.engine.set_state(state(volume=0.5, fader={"mode": "relative"}))
        self.osc.clear()
        self.move(60, 1)
        # Something else moves the track well off the bottom.
        self.clock.advance(5.0)
        self.engine.set_state(state(volume=0.3, fader={"mode": "relative"}))
        self.osc.clear()
        self.move(6)
        self.assertEqual(self.osc.msgs, [], "back to needing a walk-back")

    # --- keeping the shadow honest ----------------------------------------

    def test_it_ignores_an_echo_of_its_own_write(self):
        # The panel republishes state.json from REAPER, which lags our write.
        # Adopting the lagging figure makes the next nudge start from the
        # wrong place and step backwards -- an encoder that moves both ways,
        # in fader form.
        self.move(60, 70)
        sent = self.osc.msgs[-1][1]
        self.osc.clear()
        self.engine.set_state(state(volume=0.5, fader={"mode": "relative"}))
        self.move(80)
        self.assertEqual(self.values(), [round(sent + 10 / 127.0, 6)])

    def test_it_adopts_an_outside_change_once_the_echo_window_has_passed(self):
        # A mouse move on the track fader is the authority, and the next nudge
        # has to start from there rather than from what we last sent.
        self.move(60, 70)
        self.osc.clear()
        self.clock.advance(5.0)
        self.engine.set_state(state(volume=0.2, fader={"mode": "relative"}))
        self.move(80)
        self.assertEqual(self.values(), [round(0.2 + 10 / 127.0, 6)])


class TestFaderModeSwitching(Base):
    """The mode can change under the fader without it having moved."""

    def move(self, *raws):
        for raw in raws:
            self.engine.handle(FaderMove(raw / 127.0, raw))

    def test_switching_to_relative_does_not_nudge(self):
        # The fader's position carries across the switch, so the first move
        # after it must be measured from where it already was.
        self.move(100)
        self.engine.set_state(state(fader={"mode": "relative"}))
        self.osc.clear()
        self.move(100)
        self.assertEqual(self.osc.msgs, [], "no move, no message")
        self.move(105)
        self.assertEqual(len(self.osc.msgs), 1)

    def test_switching_to_pickup_makes_it_catch_up_again(self):
        self.engine.set_state(state(fader={"mode": "absolute"}))
        self.move(100)
        self.engine.set_state(state(volume=0.2, fader={"mode": "pickup"}))
        self.osc.clear()
        self.move(105)
        self.assertEqual(self.osc.msgs, [], "must pick up on the new mode")

    def test_leaving_relative_clears_a_pause(self):
        self.engine.set_state(state(fader={"mode": "relative",
                                           "end_zone": 3}))
        self.move(120, 126)
        self.engine.set_state(state(volume=self.engine._volume,
                                    fader={"mode": "absolute"}))
        self.osc.clear()
        self.move(127)
        self.assertEqual(len(self.osc.msgs), 1, "absolute has no end zones")

    def test_an_unknown_mode_falls_back_to_pickup(self):
        self.engine.set_state(state(volume=0.5, fader={"mode": "sideways"}))
        self.osc.clear()
        self.move(13)
        self.assertEqual(self.osc.msgs, [])


class TestFaderAddress(Base):
    """The fader is addressed by track number, not by REAPER's "current".

    DEVICE_TRACK_FOLLOWS LAST_TOUCHED means the bare /track/volume follows the
    last track touched *in the REAPER window*. Selecting a track from another
    control surface does not count, so the bare form keeps driving the track
    you have left while the panel already shows the new one.
    """

    def test_the_fader_addresses_the_selected_track_explicitly(self):
        self.engine.set_state(state(track=9, volume=0.5))
        self.engine.handle(FaderMove(0.5, 64))
        self.osc.clear()
        self.engine.handle(FaderMove(0.6, 76))
        self.assertEqual(self.osc.msgs[-1], ("/track/9/volume", 0.6))

    def test_the_master_track_keeps_the_bare_address(self):
        # Track 0 is the master, which OSC addresses differently; the bare
        # form still reaches it.
        self.engine.set_state(state(track=0, volume=0.5))
        self.engine.handle(FaderMove(0.5, 64))
        self.osc.clear()
        self.engine.handle(FaderMove(0.6, 76))
        self.assertEqual(self.osc.msgs[-1][0], "/track/volume")

    def test_other_tracks_volume_feedback_is_ignored(self):
        # A wide bank means REAPER sends /track/<N>/volume for every track.
        # Seeding pickup from whichever one moved last would put the fader's
        # take-over point on a completely unrelated level.
        self.engine.set_state(state(track=3, volume=0.5))
        self.engine.on_feedback([Message("/track/57/volume", (0.9,))])
        self.engine.handle(FaderMove(0.5, 64))
        self.assertTrue(self.osc.msgs, "0.5 must still be the pickup point")

    def test_the_selected_tracks_volume_feedback_is_adopted(self):
        self.engine.set_state(state(track=3, volume=0.1))
        self.engine.on_feedback([Message("/track/3/volume", (0.8,))])
        self.osc.clear()
        self.engine.handle(FaderMove(0.79, 100))
        self.assertEqual(self.osc.msgs[-1], ("/track/3/volume", 0.79))


class TestFocusedTrack(Base):
    """Writes go to the track the plugin is ON, not the track selected.

    The panel publishes both: track.index is the selection, which the fader
    follows deliberately, and fx.track is where the focused plugin lives.
    """

    def test_a_write_addresses_the_plugins_own_track(self):
        self.engine.set_state(state(track=3, fx_track=7))
        self.osc.clear()
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        addr, _ = self.osc.msgs[0]
        self.assertEqual(addr, "/track/7/fx/1/fxparam/13/value")

    def test_selecting_another_track_does_not_move_the_target(self):
        self.engine.set_state(state(track=7, fx_track=7))
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        before, _ = self.osc.msgs[0]
        # Same plugin still focused; the user just clicked a different track.
        self.engine.set_state(state(track=1, fx_track=7))
        self.osc.clear()
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        after, _ = self.osc.msgs[0]
        self.assertEqual(before, after,
                         "the selection must not redirect the encoders")

    def test_an_old_panel_without_fx_track_falls_back_to_the_selection(self):
        doc = {
            "version": 1, "seq": 1, "active": True,
            "track": {"index": 3, "name": "Vox", "volume": 0.5},
            "fx": {"index": 0, "ident": "x", "name": "y"},   # no "track"
            "layers": {"A": {"encoders": [
                {"param": 12, "name": "Thr", "value": 0.5, "default": 0.25}]}},
            "reserved": [],
        }
        self.engine.set_state(parse(json.dumps(doc)))
        self.osc.clear()
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        self.assertEqual(self.osc.msgs[0][0], "/track/3/fx/1/fxparam/13/value")


class TestLayers(Base):
    def test_layer_b_uses_its_own_slots(self):
        self.engine.set_state(state(
            encoders=[{"param": 1, "name": "a", "value": 0.5, "default": 0}],
            layer_b=[{"param": 77, "name": "z", "value": 0.5, "default": 0}]))
        self.osc.clear()
        self.engine.handle(EncoderTurn(0, "B", delta=1))
        self.assertEqual(self.osc.msgs[0][0], "/track/3/fx/1/fxparam/78/value")

    def test_an_event_from_a_layer_switches_the_display_to_it(self):
        # The hardware switches layers by itself and says so only by changing
        # which CC numbers it sends, so the first event from a layer IS the
        # notification that the device moved there.
        self.engine.set_state(state(
            layer_b=[{"param": 77, "name": "z", "value": 0.5, "default": 0}]))
        self.midi.clear()
        self.engine.handle(EncoderTurn(0, "B", delta=1))
        self.assertTrue(self.midi.msgs, "LEDs must repaint for the new layer")

    def test_layer_change_notifies_the_panel(self):
        s = state(layer_b=[{"param": 77, "name": "z", "value": 0.5,
                            "default": 0}])
        doc = json.loads(json.dumps({
            "version": 1, "seq": 9, "active": True,
            "track": {"index": 3, "name": "V", "volume": 0.5},
            "fx": {"index": 0, "ident": "x", "name": "y"},
            "layers": {"B": {"encoders": [
                {"param": 77, "name": "z", "value": 0.5, "default": 0}]}},
            "reserved": [],
            "notify": {"layer_a": "_RSA", "layer_b": "_RSB"},
        }))
        self.engine.set_state(parse(json.dumps(doc)))
        self.osc.clear()
        self.engine.handle(EncoderTurn(0, "B", delta=1))
        self.assertIn("/action/_RSB", self.osc.msgs)

    def test_layer_is_announced_once_not_every_event(self):
        doc = {
            "version": 1, "seq": 9, "active": True,
            "track": {"index": 3, "name": "V", "volume": 0.5},
            "fx": {"index": 0, "ident": "x", "name": "y"},
            "layers": {"B": {"encoders": [
                {"param": 77, "name": "z", "value": 0.5, "default": 0}]}},
            "reserved": [],
            "notify": {"layer_a": "_RSA", "layer_b": "_RSB"},
        }
        self.engine.set_state(parse(json.dumps(doc)))
        self.osc.clear()
        for _ in range(4):
            self.engine.handle(EncoderTurn(0, "B", delta=1))
        fired = [m for m in self.osc.msgs if m == "/action/_RSB"]
        self.assertEqual(len(fired), 1, "one announcement per switch")


# What REAPER puts in front of a burst of parameter values, captured from the
# journal: the name has no "VST3: " kind of prefix, and the number is the
# 1-based position in the chain.
def focus(number=1, name="ReaComp", track=None):
    out = [Message("/fx/name", (name,)),
           Message("/fx/number/str", (str(number),))]
    if track is not None:
        out.insert(0, Message("/track/number/str", (str(track),)))
    return out


class TestFeedback(Base):
    def test_mouse_move_in_reaper_updates_the_ring(self):
        self.engine.on_feedback(focus() + [Message("/fxparam/13/value", (0.75,))])
        self.assertIn(ring_position(0, 0.75, "A"), self.midi.msgs)

    def test_feedback_updates_the_shadow_so_turns_continue_from_it(self):
        self.engine.on_feedback(focus() + [Message("/fxparam/13/value", (0.75,))])
        self.osc.clear()
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        self.assertAlmostEqual(self.osc.msgs[0][1], 0.75 + 1 / 128)

    def test_the_announcement_holds_for_later_bursts(self):
        # Moving a knob with the mouse sends the value alone: REAPER says what
        # is focused when that changes, not on every message.
        self.engine.on_feedback(focus())
        self.engine.on_feedback([Message("/fxparam/13/value", (0.75,))])
        self.assertIn(ring_position(0, 0.75, "A"), self.midi.msgs)

    def test_volume_feedback_seeds_pickup_but_not_while_engaged(self):
        self.engine.on_feedback([Message("/track/3/volume", (0.8,))])
        self.engine.handle(FaderMove(0.79, 100))
        self.assertEqual(self.osc.msgs[-1], ("/track/3/volume", 0.79))

    def test_a_burst_for_another_plugin_is_not_adopted(self):
        """The bug this whole gate exists for.

        Changing the focused plugin makes REAPER send every parameter of the
        new FX, while the panel republishes state.json over a different path.
        The burst arrives first. Adopting it against the slots still describing
        the old plugin writes one plugin's values into another's rings, and the
        1Hz repaint then keeps them there.
        """
        self.engine.on_feedback(focus(number=2, name="ReaEQ (Cockos)")
                                + [Message("/fxparam/13/value", (0.75,))])
        self.assertEqual(self.midi.msgs, [], "must not touch a single lamp")
        self.osc.clear()
        self.engine.handle(EncoderTurn(0, "A", delta=1))
        self.assertAlmostEqual(self.osc.msgs[0][1], 0.5 + 1 / 128,
                               msg="the shadow must not have moved either")

    def test_the_panel_catching_up_reopens_the_gate(self):
        self.engine.on_feedback(focus(number=2, name="ReaEQ (Cockos)"))
        self.engine.set_state(parse(json.dumps({
            "version": 1, "seq": 2, "active": True,
            "track": {"index": 3, "name": "Vox", "volume": 0.5},
            "fx": {"index": 1, "ident": "ReaEQ", "name": "VST: ReaEQ (Cockos)"},
            "layers": {"A": {"encoders": [
                {"param": 12, "name": "Freq", "value": 0.5, "default": 0.25}]}},
            "reserved": [],
        })))
        self.midi.clear()
        self.engine.on_feedback([Message("/fxparam/13/value", (0.75,))])
        self.assertIn(ring_position(0, 0.75, "A"), self.midi.msgs)

    def test_the_selected_track_does_not_gate_the_feedback(self):
        # /track/number/str follows the SELECTION; this feedback is about the
        # FOCUSED FX, which may be on another track. Gating one on the other
        # silently held the rings off whenever they differed.
        self.engine.on_feedback(focus(track=4)
                                + [Message("/fxparam/13/value", (0.75,))])
        self.assertIn(ring_position(0, 0.75, "A"), self.midi.msgs)

    def test_nothing_is_adopted_before_reaper_has_said_what_is_focused(self):
        # state.json is the authority until then, and it carries every value
        # the panel resolves -- so this costs a frame, not correctness.
        self.engine.on_feedback([Message("/fxparam/13/value", (0.75,))])
        self.assertEqual(self.midi.msgs, [])

    def test_unrelated_feedback_is_ignored(self):
        for msg in [Message("/fx/name", ("ReaComp",)),
                    Message("/fxparam/13/value", ()),
                    Message("/fxparam/x/value", (0.5,)),
                    Message("/nonsense", (1.0,))]:
            self.engine.on_feedback([msg])   # must not raise


if __name__ == "__main__":
    unittest.main()

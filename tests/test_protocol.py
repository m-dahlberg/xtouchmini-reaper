import unittest

from xtouchmini import protocol as P
from xtouchmini.protocol import Encoding, LedState


class TestEncoderEncodings(unittest.TestCase):
    """The three relative modes the X-TOUCH Editor offers, plus absolute."""

    CASES = {
        Encoding.RELATIVE_1: {1: 1, 2: 2, 63: 63, 127: -1, 126: -2, 65: -63},
        Encoding.RELATIVE_2: {65: 1, 66: 2, 64: 0, 63: -1, 62: -2, 1: -63},
        Encoding.RELATIVE_3: {1: 1, 2: 2, 65: -1, 66: -2, 63: 63},
    }

    def test_each_encoding(self):
        for encoding, table in self.CASES.items():
            for raw, expected in table.items():
                with self.subTest(encoding=encoding, raw=raw):
                    self.assertEqual(P.decode_delta(raw, encoding), expected)

    def test_relative_modes_agree_on_small_steps(self):
        # Whichever the editor ends up set to, +1/-1 must mean the same thing.
        for encoding in self.CASES:
            plus = [v for v, d in self.CASES[encoding].items() if d == 1][0]
            minus = [v for v, d in self.CASES[encoding].items() if d == -1][0]
            self.assertEqual(P.decode_delta(plus, encoding), 1)
            self.assertEqual(P.decode_delta(minus, encoding), -1)

    def test_absolute_reports_position_not_delta(self):
        event = P.decode([P.CC, 1, 35], Encoding.ABSOLUTE)
        self.assertEqual(event.absolute, 35)
        self.assertEqual(event.delta, 0)


class TestDecode(unittest.TestCase):
    def test_all_eight_encoders(self):
        for i in range(P.ENCODERS):
            event = P.decode([P.CC, 1 + i, 1], Encoding.RELATIVE_1)
            self.assertEqual(event.index, i)
            self.assertEqual(event.delta, 1)

    def test_encoder_push_is_notes_0_to_7(self):
        for i in range(P.ENCODERS):
            down = P.decode([P.NOTE_ON, i, 127], Encoding.RELATIVE_1)
            self.assertIsInstance(down, P.EncoderPush)
            self.assertEqual((down.index, down.pressed), (i, True))
            up = P.decode([P.NOTE_OFF, i, 0], Encoding.RELATIVE_1)
            self.assertEqual((up.index, up.pressed), (i, False))

    def test_buttons_are_notes_8_to_23(self):
        for i in range(P.BUTTONS):
            event = P.decode([P.NOTE_ON, 8 + i, 127], Encoding.RELATIVE_1)
            self.assertIsInstance(event, P.ButtonPress)
            self.assertEqual(event.index, i)

    def test_note_on_with_zero_velocity_is_a_release(self):
        # Toggle-mode buttons and some hosts use this instead of note-off.
        event = P.decode([P.NOTE_ON, 8, 0], Encoding.RELATIVE_1)
        self.assertFalse(event.pressed)

    def test_fader_is_normalised(self):
        self.assertAlmostEqual(P.decode([P.CC, 9, 0], Encoding.RELATIVE_1).value, 0.0)
        self.assertAlmostEqual(P.decode([P.CC, 9, 127], Encoding.RELATIVE_1).value, 1.0)

    def test_other_channels_are_ignored(self):
        # The device only ever uses the global channel; anything else is some
        # other device on a shared port and must not move a parameter.
        for status in (0xB0, 0x90, 0xB5):
            self.assertIsNone(P.decode([status, 1, 64], Encoding.RELATIVE_1))

    def test_junk_is_ignored(self):
        for msg in ([], [0xF0], [P.CC, 99, 0], [P.CC], [P.NOTE_ON, 99, 1]):
            self.assertIsNone(P.decode(msg, Encoding.RELATIVE_1))


class TestButtonLedNumbers(unittest.TestCase):
    """A button's lamp listens on the same note the button sends.

    Measured one range at a time with the port held open: notes 8-15 light the
    top row, 16-23 the bottom. The Quick Start Guide's RX table claims 0-7 and
    8-15, which is wrong for this unit -- note 0 lights nothing at all, because
    notes 0-7 are the encoder pushes and those have no lamps.
    """

    def test_read_and_write_use_the_same_note(self):
        for i in range(P.BUTTONS):
            sent = P.decode([P.NOTE_ON, 8 + i, 127], Encoding.RELATIVE_1)
            led = P.button_led(sent.index, LedState.ON)
            self.assertEqual(led[1], 8 + i,
                             "a button's lamp uses the note it sends")

    def test_the_rows(self):
        # Top row is buttons 1-8, bottom row 9-16.
        self.assertEqual(P.button_led(0, LedState.ON)[1], 8)
        self.assertEqual(P.button_led(7, LedState.ON)[1], 15)
        self.assertEqual(P.button_led(8, LedState.ON)[1], 16)
        self.assertEqual(P.button_led(15, LedState.ON)[1], 23)

    def test_lamps_never_use_the_encoder_push_notes(self):
        # Notes 0-7 are the encoder pushes and address nothing; sending a lamp
        # there is silently ignored by the device.
        for i in range(P.BUTTONS):
            self.assertNotIn(P.button_led(i, LedState.ON)[1], range(0, 8))

    def test_led_states(self):
        self.assertEqual(P.button_led(3, LedState.OFF)[2], 0)
        self.assertEqual(P.button_led(3, LedState.ON)[2], 1)
        self.assertEqual(P.button_led(3, LedState.BLINK)[2], 2)


class TestRings(unittest.TestCase):
    def test_position_is_a_0_127_value_not_the_manuals_1_13(self):
        # Measured on the unit with a static staircase across the eight rings:
        # 0-127 spreads evenly over the thirteen LEDs. Sending 1-13 instead
        # crams the whole scale into the first LED or two, which reads as an
        # unresponsive ring rather than a mis-scaled one.
        self.assertEqual(P.ring_position(0, 0.0, "A")[2], 1)
        self.assertEqual(P.ring_position(0, 1.0, "A")[2], 127)
        self.assertEqual(P.ring_position(0, 0.5, "A")[2], 64)

    def test_the_scale_is_monotonic_and_uses_the_whole_range(self):
        seen = [P.ring_position(0, i / 100, "A")[2] for i in range(101)]
        self.assertEqual(seen, sorted(seen))
        self.assertEqual((min(seen), max(seen)), (1, 127))

    def test_unassigned_is_distinguishable_from_zero(self):
        # 0 means "all LEDs off" -- verified on the unit, a ring sent 0 is
        # completely dark. A real zero value is sent as 1, the lowest lit
        # value, so an unassigned encoder and a parameter at its minimum do
        # not look alike.
        self.assertEqual(P.ring_position(0, None, "A")[2], 0)
        self.assertNotEqual(P.ring_position(0, 0.0, "A")[2], 0)

    def test_out_of_range_values_are_clamped(self):
        self.assertEqual(P.ring_position(0, -5.0, "A")[2], 1)
        self.assertEqual(P.ring_position(0, 5.0, "A")[2], 127)

    def test_each_encoder_addresses_its_own_cc(self):
        # Measured: a ring listens on the SAME CC its encoder sends, so the
        # numbers move with the layer. The manual's "position is CC 9-16" is
        # wrong, and wrong silently: CC 9 and 10 are the two faders, which
        # have no rings, and CC 11-16 are six of layer B's rings. Every ring
        # message the daemon sent went there.
        for i in range(P.ENCODERS):
            self.assertEqual(P.ring_position(i, 0.5, "A")[1], 1 + i)
            self.assertEqual(P.ring_position(i, 0.5, "B")[1], 11 + i)

    def test_a_ring_is_addressed_by_its_own_encoders_cc(self):
        for layer in P.LAYERS:
            for i in range(P.ENCODERS):
                self.assertEqual(P.ring_cc(i, layer.name), layer.encoder_cc[i])

    def test_no_ring_lands_on_a_fader(self):
        faders = {layer.fader_cc for layer in P.LAYERS}
        for layer in P.LAYERS:
            for i in range(P.ENCODERS):
                self.assertNotIn(P.ring_cc(i, layer.name), faders)

    def test_the_two_layers_never_collide(self):
        a = {P.ring_cc(i, "A") for i in range(P.ENCODERS)}
        b = {P.ring_cc(i, "B") for i in range(P.ENCODERS)}
        self.assertEqual(a & b, set())

    def test_ring_all(self):
        self.assertEqual(P.ring_all(0, True, "A")[2], 127)
        self.assertEqual(P.ring_all(0, False, "A")[2], 0)


class TestLayers(unittest.TestCase):
    """The layer is recovered from the number, because only the number knows."""

    def test_both_layers_are_measured(self):
        for layer in P.LAYERS:
            self.assertTrue(layer.measured, f"layer {layer.name}")

    def test_layer_b_fader_is_cc_10_not_the_pattern_guess(self):
        # Regression on a real wrong guess. The encoder banks are far apart
        # (1-8 and 11-18), so extrapolating put the layer B fader at CC 19;
        # it is actually CC 10, adjacent to layer A's CC 9. The wrong value
        # drove a different control with no error.
        self.assertEqual(P.TX_LAYER_B.fader_cc, 10)
        self.assertEqual(P.TX_LAYER_A.fader_cc, 9)
        event = P.decode([P.CC, 10, 64], Encoding.RELATIVE_1)
        self.assertIsInstance(event, P.FaderMove)
        self.assertEqual(event.layer, P.LAYER_B)

    def test_layers_do_not_overlap(self):
        # Overlapping numbers would make the layer unrecoverable, and the
        # daemon would drive the wrong page's parameter.
        a, b = P.TX_LAYER_A, P.TX_LAYER_B
        self.assertFalse(set(a.encoder_cc) & set(b.encoder_cc))
        self.assertFalse(set(a.push_note) & set(b.push_note))
        self.assertFalse(set(a.button_note) & set(b.button_note))
        self.assertNotEqual(a.fader_cc, b.fader_cc)
        self.assertNotIn(a.fader_cc, b.encoder_cc)
        self.assertNotIn(b.fader_cc, a.encoder_cc)

    def test_within_a_layer_encoders_and_fader_are_distinct(self):
        for layer in P.LAYERS:
            self.assertNotIn(layer.fader_cc, layer.encoder_cc)
            self.assertFalse(set(layer.push_note) & set(layer.button_note))

    def test_each_layer_decodes_to_its_own_name(self):
        for layer in P.LAYERS:
            enc = P.decode([P.CC, layer.encoder_cc.start, 1], Encoding.RELATIVE_1)
            self.assertEqual((enc.index, enc.layer), (0, layer.name))
            btn = P.decode([P.NOTE_ON, layer.button_note.start, 127],
                           Encoding.RELATIVE_1)
            self.assertEqual((btn.index, btn.layer), (0, layer.name))
            push = P.decode([P.NOTE_ON, layer.push_note.start, 127],
                            Encoding.RELATIVE_1)
            self.assertEqual((push.index, push.layer), (0, layer.name))
            fader = P.decode([P.CC, layer.fader_cc, 64], Encoding.RELATIVE_1)
            self.assertEqual(fader.layer, layer.name)

    def test_every_layer_covers_the_full_control_count(self):
        for layer in P.LAYERS:
            self.assertEqual(len(layer.encoder_cc), P.ENCODERS)
            self.assertEqual(len(layer.push_note), P.ENCODERS)
            self.assertEqual(len(layer.button_note), P.BUTTONS)


class TestGlobals(unittest.TestCase):
    def test_layer_select(self):
        self.assertEqual(P.select_layer("A"), [P.PROGRAM_CHANGE, 0])
        self.assertEqual(P.select_layer("b"), [P.PROGRAM_CHANGE, 1])

    def test_mode_select(self):
        self.assertEqual(P.select_mode(False), [P.CC, 127, 0])
        self.assertEqual(P.select_mode(True), [P.CC, 127, 1])

    def test_everything_is_on_the_global_channel(self):
        for msg in (P.ring_position(0, 0.5, "A"), P.ring_position(0, 0.5, "B"),
                    P.button_led(0, LedState.ON),
                    P.select_layer("A"), P.select_mode(False)):
            self.assertEqual(msg[0] & 0x0F, P.GLOBAL_CHANNEL - 1)


if __name__ == "__main__":
    unittest.main()

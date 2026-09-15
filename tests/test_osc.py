import struct
import unittest

from xtouchmini.osc import Message, decode, encode


class TestEncode(unittest.TestCase):
    def test_every_message_is_four_byte_aligned(self):
        # OSC requires it and REAPER quietly drops packets that are not.
        for args in [(0.5,), ("ReaComp",), (7,), (), (1.0, "x", 3),
                     ("a" * 3,), ("a" * 4,), ("a" * 5,)]:
            with self.subTest(args=args):
                self.assertEqual(len(encode("/track/1/volume", *args)) % 4, 0)

    def test_type_tags(self):
        self.assertIn(b",f", encode("/a", 1.0))
        self.assertIn(b",i", encode("/a", 1))
        self.assertIn(b",s", encode("/a", "x"))
        self.assertIn(b",fsi", encode("/a", 1.0, "x", 2))

    def test_no_arg_message_still_has_a_tag_string(self):
        self.assertEqual(decode(encode("/play")), [Message("/play", ())])

    def test_bool_is_refused(self):
        # OSC's T/F tags carry no payload; sending a bool where REAPER expects
        # a float would arrive as a type mismatch rather than 0.0/1.0.
        with self.assertRaises(TypeError):
            encode("/a", True)

    def test_address_terminator_is_padded_not_truncated(self):
        # "/abc" is 4 bytes, so the null terminator forces a whole extra word.
        self.assertTrue(encode("/abc").startswith(b"/abc\0\0\0\0"))


class TestDecode(unittest.TestCase):
    def test_round_trip(self):
        for address, args in [
            ("/track/3/fx/1/fxparam/12/value", (0.25,)),
            ("/fx/name", ("VST: ReaComp (Cockos)",)),
            ("/device/fxparam/count", (128,)),
            ("/a", (1.5, "x", 7)),
        ]:
            with self.subTest(address=address):
                got = decode(encode(address, *args))
                self.assertEqual(len(got), 1)
                self.assertEqual(got[0].address, address)
                for a, b in zip(got[0].args, args):
                    if isinstance(b, float):
                        self.assertAlmostEqual(a, b, places=6)
                    else:
                        self.assertEqual(a, b)

    def test_bundle_is_flattened(self):
        # REAPER batches feedback into bundles; a decoder that only understood
        # bare messages would see nothing during an FX focus change.
        inner = [encode("/fxparam/1/value", 0.1),
                 encode("/fxparam/2/value", 0.2)]
        packet = b"#bundle\0" + b"\0" * 8
        for m in inner:
            packet += struct.pack(">i", len(m)) + m
        got = decode(packet)
        self.assertEqual([m.address for m in got],
                         ["/fxparam/1/value", "/fxparam/2/value"])

    def test_nested_bundle(self):
        leaf = encode("/x", 1.0)
        inner = b"#bundle\0" + b"\0" * 8 + struct.pack(">i", len(leaf)) + leaf
        outer = b"#bundle\0" + b"\0" * 8 + struct.pack(">i", len(inner)) + inner
        self.assertEqual([m.address for m in decode(outer)], ["/x"])

    def test_garbage_does_not_raise(self):
        for packet in [b"", b"nonsense", b"/no-tags\0\0\0\0",
                       b"#bundle\0" + b"\0" * 8 + struct.pack(">i", 999),
                       b"/a\0\0,f\0\0" + b"\x00"]:  # truncated float payload
            with self.subTest(packet=packet):
                try:
                    decode(packet)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"decode raised {exc!r}")

    def test_unknown_type_tag_stops_rather_than_guessing(self):
        # Past an unknown tag every offset is guesswork, so we keep what we
        # parsed and stop instead of returning invented values.
        packet = encode("/a", 1.0).replace(b",f", b",fb", 1)
        got = decode(packet)
        self.assertEqual(got[0].address, "/a")


if __name__ == "__main__":
    unittest.main()

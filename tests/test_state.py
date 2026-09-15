import json
import os
import tempfile
import unittest
from pathlib import Path

from xtouchmini.state import State, StateFile, parse

BASE = {
    "version": 1,
    "seq": 1,
    "active": True,
    "track": {"index": 3, "name": "Vox", "volume": 0.5},
    "fx": {"index": 0, "ident": "ReaComp.vst3", "name": "VST: ReaComp"},
    "layers": {
        "A": {
            "encoders": [{"param": 12, "name": "Threshold", "value": 0.4}],
            "buttons": [{"param": 5, "name": "Bypass", "mode": "momentary"}],
        },
        "B": {"encoders": [{"param": 1, "name": "Ratio"}], "buttons": []},
    },
    "reserved": [{"kind": "builtin", "id": "bank_prev"},
                 {"kind": "action", "id": "_RS123"}],
}


def doc(**over):
    d = json.loads(json.dumps(BASE))
    d.update(over)
    return json.dumps(d)


class TestParse(unittest.TestCase):
    def test_reads_slots_from_both_layers(self):
        s = parse(doc())
        self.assertEqual(s.slot("A", "encoders", 0).name, "Threshold")
        self.assertEqual(s.slot("B", "encoders", 0).name, "Ratio")

    def test_unassigned_slots_are_none(self):
        s = parse(doc())
        self.assertIsNone(s.slot("A", "encoders", 7))
        self.assertIsNone(s.slot("B", "buttons", 0))

    def test_slots_are_dead_when_inactive(self):
        # The strict-focus rule is enforced by the panel; the daemon must obey
        # it even though the mapping is still sitting in the file.
        s = parse(doc(active=False))
        self.assertIsNone(s.slot("A", "encoders", 0))

    def test_active_requires_a_real_fx_index(self):
        d = json.loads(doc())
        d["fx"]["index"] = -1
        self.assertFalse(parse(json.dumps(d)).active)

    def test_reserved_survives_inactivity(self):
        # Reserved buttons and the fader keep working with no plugin focused.
        s = parse(doc(active=False))
        self.assertEqual(s.reserved_at(0).id, "bank_prev")
        self.assertEqual(s.track.volume, 0.5)

    def test_bad_reserved_entry_is_dropped_not_guessed(self):
        d = json.loads(doc())
        d["reserved"] = [{"kind": "nonsense", "id": "x"}, {"id": "no-kind"}]
        s = parse(json.dumps(d))
        self.assertIsNone(s.reserved_at(0))
        self.assertIsNone(s.reserved_at(1))

    def test_values_are_clamped(self):
        d = json.loads(doc())
        d["layers"]["A"]["encoders"][0]["value"] = 4.2
        self.assertEqual(parse(json.dumps(d)).slot("A", "encoders", 0).value, 1.0)

    def test_malformed_input_is_inert(self):
        # Regression: a field arriving with the wrong *type* used to raise out
        # of parse and take the daemon's poll loop down with it.
        for text in ["", "{", "null", "[]", '{"version": 99}',
                     '{"version": 1, "layers": "nope"}',
                     '{"version": 1, "track": "nope"}',
                     '{"version": 1, "fx": []}',
                     '{"version": 1, "seq": "x"}',
                     '{"version": 1, "reserved": "nope"}',
                     '{"version": 1, "layers": {"A": "nope"}}',
                     '{"version": 1, "layers": {"A": {"encoders": "nope"}}}',
                     '{"version": 1, "track": {"index": "x", "volume": "y"}}']:
            with self.subTest(text=text):
                self.assertFalse(parse(text).active)

    def test_slot_with_non_numeric_param_is_dropped(self):
        d = json.loads(doc())
        d["layers"]["A"]["encoders"] = [{"param": "twelve", "name": "T"}]
        self.assertIsNone(parse(json.dumps(d)).slot("A", "encoders", 0))

    def test_negative_param_index_is_rejected(self):
        d = json.loads(doc())
        d["layers"]["A"]["encoders"][0]["param"] = -1
        self.assertIsNone(parse(json.dumps(d)).slot("A", "encoders", 0))


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"
        self.addCleanup(self.dir.cleanup)

    def write(self, text):
        self.path.write_text(text)
        # Force a distinct mtime; the test writes faster than the clock ticks.
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def test_missing_file_is_inert(self):
        f = StateFile(self.path)
        self.assertFalse(f.poll())
        self.assertFalse(f.state.active)

    def test_picks_up_a_new_file(self):
        f = StateFile(self.path)
        self.write(doc())
        self.assertTrue(f.poll())
        self.assertEqual(f.state.fx.ident, "ReaComp.vst3")

    def test_unchanged_file_is_not_reparsed(self):
        f = StateFile(self.path)
        self.write(doc())
        self.assertTrue(f.poll())
        self.assertFalse(f.poll())

    def test_deleted_file_deactivates(self):
        f = StateFile(self.path)
        self.write(doc())
        f.poll()
        self.path.unlink()
        self.assertTrue(f.poll())
        self.assertFalse(f.state.active)

    def test_shadow_value_update(self):
        f = StateFile(self.path)
        self.write(doc())
        f.poll()
        f.note_value("A", "encoders", 0, 0.9)
        self.assertAlmostEqual(f.state.slot("A", "encoders", 0).value, 0.9)
        # and the name survives the replace
        self.assertEqual(f.state.slot("A", "encoders", 0).name, "Threshold")


if __name__ == "__main__":
    unittest.main()
